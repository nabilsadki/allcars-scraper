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
refresh  : rafraichit les fiches les plus anciennes, selectionnees en base
mixed    : rescan de la fenetre de liste, puis refresh d'un lot en base
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

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
                    if ratio > 3:
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
    seen_count = 0
    run_started = datetime.now().replace(microsecond=0)
    if getattr(site, "use_slices", True) and hasattr(site, "slices"):
        # certains adaptateurs ont besoin de la progression pendant qu'ils
        # construisent les tranches (sondages reseau : avito)
        try:
            site.progress = progress
        except Exception:
            pass
        slices = site.slices(category, client)
        if getattr(site, "log_slices", ""):
            print(f"  {site.log_slices}", flush=True)
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
            if isinstance(ad, dict) and not ad.get("list_time") and not ad.get("publish_date"):
                # sites sans date affichee : on attribue une date synthetique
                # strictement decroissante suivant l'ordre de lecture de la
                # liste (premier lu = le plus recent)
                synth = run_started - timedelta(seconds=seen_count)
                ad["list_time"] = synth.isoformat(sep=" ")
                ad["publish_date"] = ad["list_time"]
            seen_count += 1
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
        # Au-dela de la premiere page d'une tranche, une page vide signifie
        # "fin de la tranche" : Avito renvoie une page vide des que le
        # plafond de pagination est atteint. Avec des tranches fines
        # (~40 annonces) attendre 4 minutes a chaque fin ruinerait le run.
        # On garde la patience quand on est encore loin de la fin (page
        # vide = incident passager), on abrege quand on est au bout.
        near_end = bool(total) and offset > (total or 0) / 8
        attempts = 2 if near_end else 7
        # certaines pages renvoient une variante incomplete : on reessaye
        try:
            # "pages" en flax (401/502) : on reessay souvent pour eviter d'abandonner
            # prematurerement quand le site ressent le referrer.
            for attempt in range(1, attempts + 1):
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
                if attempt < attempts:
                    client.log(f"  ! page vide ({attempt}/{attempts}) - nouvel essai")
                    time.sleep(min(60, 8 * attempt) if attempts > 2 else 3)
        except Exception as e:
            if "HTTP 404" in str(e):
                # page au-dela de la fin (siaracash, tomobila...) : normal
                client.log(f"  . fin de liste (404 a l'offset {offset})")
                break
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
        # plafond de pagination du site (ex. avito : ~total/36)
        try:
            cap = site.max_offset(total)
        except Exception:
            cap = None
        if cap is not None and offset > cap:
            if verbose:
                print(f"  plafond de pagination atteint ({offset} > {cap})",
                      flush=True)
            break



#: cles qui prouvent qu'une page a vraiment livre le contenu d'une fiche.
#: Volontairement sans `seller`, `details_fetched`, `ad_id`, `url` : plusieurs
#: adaptateurs les renseignent meme quand la page ne contient rien.
DETAIL_CONTENT_KEYS = frozenset({
    "title", "description", "price", "price_str", "year", "mileage_km", "km",
    "fuel", "gearbox", "gear", "brand", "model", "body_type", "body", "doors",
    "color", "version", "fiscal_power", "cv", "phone", "images", "city",
    "cond", "origin", "first_hand", "options", "old_price",
})


def _same_target(url: str, final: str, *identifiants: Any) -> bool:
    """La page effectivement lue correspond-elle a l'URL demandee ?

    Les sites renvoient parfois une redirection (302 -> accueil, -> autre
    annonce, -> page "neuf") qui aboutit a un HTTP 200 : sans ce controle,
    la fiche etait marquee vue alors que la page n'avait rien a voir.
    """
    if not final:
        return True

    def _norm(u: str) -> tuple[str, str]:
        p = urlsplit(u or "")
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host, unquote(p.path or "").rstrip("/").lower()

    if _norm(url) == _norm(final):
        return True
    # une redirection qui conserve l'identifiant de l'annonce reste valable
    # (http->https, ajout de slug/ville, normalisation de casse...).
    # `list_id` compte aussi : sur Avito l'`ad_id` interne differe de l'id
    # present dans l'URL, qui est stocke dans `list_id`.
    fin = unquote(final)
    for ident in identifiants:
        if ident is not None and str(ident) and str(ident) in fin:
            return True
    return False


def fetch_detail(site: SiteAdapter, client, ad: dict) -> dict:
    """Recupere la fiche detail d'une annonce (si le site le permet)."""
    url = site.detail_url(ad)
    if not url:
        return ad
    try:
        html = client.get(url)
        final = getattr(client, "last_url", "") or url
        if not _same_target(url, final, ad.get("ad_id"), ad.get("list_id")):
            client.log(f"  ! fiche {ad.get('ad_id')} : page differente "
                       f"({final[:90]}) - ignoree")
            return ad
        full = site.parse_detail(html, ad)
    except Exception as e:
        # fiche introuvable : l'annonce est surement supprimee du site
        if ("HTTP 404" in str(e) or "HTTP 410" in str(e)):
            out = dict(ad)
            out["details_fetched"] = True
            out["is_active"] = False
            out["url"] = url
            return out
        client.log(f"  ! detail en echec ({type(e).__name__}: {e})")
        return ad
    if not full:
        return ad
    # page 200 mais vide (annonce retiree, gabarit generique, redirection
    # douce) : aucune donnee de fiche -> on ne marque pas l'annonce vue.
    if not (DETAIL_CONTENT_KEYS & set(full)):
        client.log(f"  ! fiche {ad.get('ad_id')} : page sans contenu - ignoree")
        return ad
    merged = dict(ad)
    for k, v in full.items():
        if v is None or v == [] or v == {}:
            continue
        merged[k] = v
    # Laisser le connecteur fournir une URL publique (ex. kia, qui scrape
    # l'API mais dont le lien final doit pointer vers la fiche du site).
    if not merged.get("url"):
        merged["url"] = url
    merged["details_fetched"] = True
    if merged.get("images"):
        merged["images"] = site.clean_images(merged["images"], merged)
    merged["nb_images"] = len(merged.get("images") or [])
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
                if ad.get("details_fetched"):
                    n_details += 1
            _apply_notes(ad, market=market, config=config)
            if out_fh:
                out_fh.write(json.dumps(ad, ensure_ascii=False, default=str) + "\n")
            if db is not None:
                try:
                    db.upsert_ad(ad, site=site.name)
                    db.upsert_images(ad.get("ad_id"), ad.get("images"),
                                      site=site.name,
                                      only_if_empty=not ad.get("details_fetched"))
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
               run_id: Optional[int] = None, progress_mode: str = "") -> dict:
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
    progress = Progress(site.name, mode=progress_mode or "rescan", category=category)

    for ad in iter_listing(site, client, category, limit, verbose=not quiet,
                           progress=progress):
        ad_id = ad.get("ad_id")
        if ad_id is not None:
            seen.add(ad_id)
        if not ad.get("brand"):
            ad = site.enrich(ad, client)

        # 1) detection SANS ecriture : la page de liste suffit
        state, _changes = db.check(ad, site=site.name)

        # fiches manquantes : on profite du passage de liste pour detaller
        # les annonces connues mais encore incomplete, meme si rien n'a
        # change (compare les details triggering field sans faux positif).
        # fraicheur : une annonce non revue depuis plus de 4 jours doit etre
        # rescrapee (detail), meme si son contenu n'a pas change.
        stale = False
        if details and ad_id is not None:
            try:
                ls = db.last_seen_at(ad_id, site.name)
                if ls is None or (datetime.now() - ls).total_seconds() > 4 * 86400:
                    stale = True
            except Exception:
                stale = True

        need_detail = (details and ad_id is not None and
                       (not db.details_fetched(ad_id, site.name) or stale))
        if state == "unchanged":
            if need_detail:
                try:
                    ad_before = bool(ad.get("details_fetched"))
                    ad = fetch_detail(site, client, ad)
                    if ad.get("details_fetched"):
                        n_details += 1
                        _apply_notes(ad, market=market, config=config)
                        # on re-pousse l'annonce pour rafraichir last_seen_at
                        # et, le cas echeant, les colonnes enrichies.
                        try:
                            db.upsert_ad(ad, site=site.name)
                            db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name,
                                             only_if_empty=not ad.get("details_fetched"))
                        except Exception as e:
                            db.conn.rollback()
                            client.log(f"  ! ecriture stale en echec ({type(e).__name__}: {str(e)[:60]})")
                except Exception as e:
                    client.log(f"  ! detail en echec sur fiche manquante/stale "
                              f"({type(e).__name__}: {str(e)[:60]})")
            # mise a jour du last_seen pour les annonces simplement revues
            # (sinon le compteur de fraicheur ne progresse jamais)
            if not need_detail:
                try:
                    db.cur.execute(
                        "UPDATE ads SET last_seen_at = NOW(), seen_count = seen_count + 1, "
                        "is_active = 1 WHERE site = %s AND ad_id = %s",
                        (site.name, ad_id),
                    )
                except Exception:
                    try:
                        db.conn.rollback()
                    except Exception:
                        pass
            n_unchanged += 1
            continue

        if state == "new":

            n_new += 1
        else:
            n_candidates += 1

        # 2) la fiche detail fait foi
        if details:
            ad = fetch_detail(site, client, ad)
            if ad.get("details_fetched"):
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
            db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name,
                             only_if_empty=not ad.get("details_fetched"))
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

    # disparitions : uniquement si le run a couvert une fraction suffisante
    # de la categorie (la couverture exacte n'est jamais atteinte : le
    # plafond Avito est ~2,8 %, siaracash coupe a la 404, etc.). Le gate
    # reste activable au niveau du job (`--mark-missing`) et par le daemon.
    threshold = getattr(site, "mark_missing_min_coverage", 0.90)
    total = (getattr(site, "global_total", None)
             or getattr(client, "catalog_total", None) or 0)
    coverage = (len(seen) / total) if total else 0.0
    complete = bool(total) and coverage >= threshold
    n_gone = 0
    if mark_missing and complete:
        n_gone = db.mark_disappeared(list(known_before - seen), site=site.name)
        if not quiet:
            print(f"  {n_gone} annonces disparues marquees inactives "
                  f"(couverture {coverage * 100:.1f} % >= {threshold * 100:.0f} %)")
    elif mark_missing and not quiet:
        print(f"  run partiel ({len(seen)}/{total or '?'} = "
              f"{coverage * 100:.1f} %) : pas de marquage "
              f"(seuil {threshold * 100:.0f} %)")


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
        db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name,
                         only_if_empty=not ad.get("details_fetched"))
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


def run_refresh(site: SiteAdapter, client, db: Database, *,
                stale_days: int = 4, limit: int = 500, category: str = "",
                quiet: bool = False, run_id: Optional[int] = None,
                progress_mode: str = "") -> dict:
    """Rafraichit les fiches les plus anciennes, en partant de la BDD.

    Contrairement a `run_rescan` (qui part du listing du site), ce mode ne
    parcourt aucune page de liste : il selectionne les annonces directement
    en base, des plus anciennes aux plus recentes, et refait leur fiche
    detail. Il sert a entretenir le catalogue que les passes de liste ne
    revoient jamais (plafond de pagination, tranches non atteintes...).

    Priorite :
      1. les fiches sans detail (`details_fetched = 0`) ;
      2. puis `last_seen_at` croissant (les plus anciennes d'abord).

    Une fiche dont le fetch echoue (reseau, 5xx) n'est PAS marquee comme vue :
    `last_seen_at` reste inchange et elle sera reprise au run suivant.
    """
    started = datetime.now()
    t0 = time.time()
    config = load_config()
    try:
        market = MarketStats(db)
    except Exception:
        market = None

    limit = int(limit or 0) or 500
    stale_days = max(1, int(stale_days or 4))

    # Pas de filtre par categorie : selon les sites, la colonne `category`
    # contient un libelle ("Voiture d'occasion") et non le slug de listing
    # (`achat-voiture-occasion`) ; filtrer dessus ne selectionnerait rien.
    #
    # Deux branches UNION ALL plutot qu'un OR : chacune utilise l'index
    # idx_refresh (site, is_active, details_fetched, last_seen_at), la ou
    # le OR force un filesort sur tout le catalogue (~0,4 s -> ~5 ms).
    base = ("SELECT * FROM ads "
            "WHERE site = %s AND is_active = 1 AND link IS NOT NULL ")
    sql = (f"({base} AND details_fetched = 0 "
           f"ORDER BY last_seen_at ASC LIMIT %s) "
           f"UNION ALL "
           f"({base} AND details_fetched = 1 "
           f"AND last_seen_at < NOW() - INTERVAL %s DAY "
           f"ORDER BY last_seen_at ASC LIMIT %s)")
    params = [site.name, limit, site.name, stale_days, limit]

    rows = db.query(sql, tuple(params))[:limit]
    if not quiet:
        print(f"  {len(rows)} fiche(s) a rafraichir "
              f"(anciennete > {stale_days} j, plafond {limit})", flush=True)

    progress = Progress(site.name, mode=progress_mode or "refresh", category=category)
    progress.update(total=len(rows))
    n = n_failed = n_gone = 0
    for r in rows:
        ad_id = r.get("ad_id")
        if ad_id is None:
            continue
        # On repart du dernier etat complet connu (raw_json) : les champs que
        # la fiche detail ne renvoie pas sont conserves au lieu d'etre ecrases
        # par NULL (contrairement a un dict minimal ad_id/url).
        try:
            base_ad = db._previous_state(r)
            if not isinstance(base_ad, dict) or not base_ad:
                base_ad = db._state_of(r)
            base_ad = dict(base_ad)
            base_ad["ad_id"] = ad_id
            base_ad["url"] = r.get("link") or base_ad.get("url")
            base_ad.pop("details_fetched", None)
            base_ad.pop("is_active", None)
        except Exception:
            base_ad = {"ad_id": ad_id, "url": r.get("link"),
                       "brand": r.get("brand"), "model": r.get("model")}
        try:
            ad = fetch_detail(site, client, base_ad)
        except Exception as e:
            n_failed += 1
            if not quiet:
                print(f"  ! fiche {ad_id} : {type(e).__name__}: {str(e)[:80]}",
                      flush=True)
            continue

        # echec transitoire : fetch_detail rend l'annonce d'entree, sans
        # details_fetched. On ne touche pas a last_seen_at (nouvel essai
        # au prochain run).
        if not ad.get("details_fetched"):
            n_failed += 1
            continue

        if ad.get("is_active") is False:
            n_gone += 1
        if not ad.get("brand"):
            try:
                ad = site.enrich(ad, client)
            except Exception:
                pass
        _apply_notes(ad, market=market, config=config)
        try:
            db.upsert_ad(ad, site=site.name)
            if ad.get("is_active") is False:
                # 404/410 : upsert peut reactiver la ligne dans la branche
                # "inchangee" ; on force la desactivation.
                db.cur.execute(
                    "UPDATE ads SET is_active = 0, details_fetched = 1, "
                    "last_seen_at = NOW() WHERE site = %s AND ad_id = %s",
                    (site.name, ad_id),
                )
            # une fiche sans image ne doit pas effacer la galerie existante
            db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name,
                             only_if_empty=not (ad.get("images") or []))
        except Exception as e:
            n_failed += 1
            try:
                db.conn.rollback()
            except Exception:
                pass
            if not quiet:
                print(f"  ! ecriture {ad_id} : {type(e).__name__}: {str(e)[:80]}",
                      flush=True)
            continue

        n += 1
        if n % 10 == 0:
            progress.update(details=n)
            progress.stats(client, db)
        if n % 100 == 0:
            try:
                db.commit()
            except Exception:
                db.reconnect()
            if run_id:
                db.update_run(run_id, {
                    "ads_details": n,
                    "ads_updated": db.counters.get("updated", 0),
                    "ads_unchanged": db.counters.get("unchanged", 0),
                    "http_requests": client.stats["requests"],
                    "blocked": client.stats["blocked"],
                })
        if not quiet and n % 25 == 0:
            print(f"  ... {n}/{len(rows)} fiches rafraichies "
                  f"({n_gone} disparues, {n_failed} echecs)", flush=True)

    try:
        db.commit()
    except Exception:
        db.reconnect()
    progress.update(details=n)
    progress.stats(client, db)
    progress.finish("success")

    dt = time.time() - t0
    if not quiet:
        print(f"\nRefresh termine : {n}/{len(rows)} fiches rafraichies | "
              f"{n_gone} disparues | {n_failed} echecs | "
              f"{client.stats['requests']} requetes | {dt:.0f}s", flush=True)
    return {"refreshed": n, "details": n, "selected": len(rows),
            "disappeared": n_gone, "failed": n_failed,
            "seconds": dt, "started": started, "error": None}


def run_mixed(site: SiteAdapter, client, db: Database, *,
              category: str, limit: int = 0, refresh_limit: int = 0,
              stale_days: int = 4, mark_missing: bool = False,
              quiet: bool = False, run_id: Optional[int] = None) -> dict:
    """Un seul run : rescan de la fenetre de liste, puis refresh en base.

    Le rescan passe en premier : il vient de rafraichir `last_seen_at` sur sa
    fenetre, donc la selection du refresh (ORDER BY last_seen_at ASC) ne
    reprend pas ces annonces. L'inverse gaspillerait des requetes.

    `refresh_limit` est le lot de la seconde passe (0 = pas de refresh, le
    run se comporte comme un rescan). Il se calibre en fonction du temps
    restant dans le creneau : ~1,2 s par fiche mesuree.
    """
    started = datetime.now()
    t0 = time.time()

    res = run_rescan(site, client, db, category=category, limit=limit,
                     details=True, mark_missing=mark_missing, quiet=quiet,
                     run_id=run_id, progress_mode="mixed")

    # compteurs de la seule passe refresh (le rescan a deja incrementé)
    before = dict(db.counters)
    refreshed = gone_refresh = failed_refresh = 0
    if refresh_limit:
        if not quiet:
            print(f"\n=== passe refresh : {refresh_limit} fiche(s) "
                  f"(anciennete > {stale_days} j) ===", flush=True)
        res2 = run_refresh(site, client, db, stale_days=stale_days,
                           limit=refresh_limit, quiet=quiet, run_id=run_id,
                           progress_mode="mixed")
        refreshed = int(res2.get("refreshed") or 0)
        gone_refresh = int(res2.get("disappeared") or 0)
        failed_refresh = int(res2.get("failed") or 0)

    def _delta(key: str) -> int:
        return int(db.counters.get(key, 0)) - int(before.get(key, 0))

    inserted = int(res.get("new") or 0) + _delta("inserted")
    changed = int(res.get("changed") or 0) + _delta("updated")
    unchanged = int(res.get("unchanged") or 0) + _delta("unchanged")
    details = int(res.get("details") or 0) + refreshed
    disappeared = int(res.get("disappeared") or 0) + gone_refresh

    # La 2e passe a reecrit le fichier de progression avec SES compteurs
    # (mode=refresh, listed=0). On remet l'etat complet du run mixte pour
    # que l'interface affiche les deux passes, pas seulement la derniere.
    try:
        from .progress import PROGRESS_DIR
        path = os.path.join(PROGRESS_DIR, f"{site.name}.json")
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        state.update({
            "mode": "mixed",
            "status": "success",
            "listed": int(res.get("seen") or 0),
            "details": details,
            "inserted": inserted,
            "updated": changed,
            "unchanged": unchanged,
            "total": 0,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "error": None,
        })
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception:
        pass   # le suivi ne doit jamais faire echouer le scrape

    dt = time.time() - t0
    if not quiet:
        print(f"\nMixed termine : {res.get('seen', 0)} annonces vues | "
              f"{int(res.get('new') or 0)} nouvelles | "
              f"{int(res.get('changed') or 0)} modifiees | "
              f"{refreshed} fiches rafraichies | {failed_refresh} echecs | "
              f"{client.stats['requests']} requetes | {dt:.0f}s", flush=True)
    return {"seen": int(res.get("seen") or 0), "new": inserted,
            "changed": changed, "unchanged": unchanged,
            "details": details, "disappeared": disappeared,
            "refreshed": refreshed, "failed": failed_refresh,
            "seconds": dt, "started": started, "error": None}
