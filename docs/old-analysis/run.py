#!/usr/bin/env python3
"""
Scraper Avito.ma — HTTP (curl_cffi + __NEXT_DATA__), sans navigateur.

Exemples
--------
  # 200 annonces de la catégorie voitures, avec les fiches detail (telephone)
  ./run.py --category "maroc/voitures-à_vendre" --limit 200 --details --out data/voitures.jsonl

  # tout le catalogue d'une categorie, sans detail (rapide)
  ./run.py --category "maroc/voitures-à_vendre" --out data/voitures.jsonl

  # vers MySQL
  ./run.py --category "maroc/voitures-à_vendre" --db --details
"""

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrapper.sites.avito_http import AvitoClient  # noqa: E402

CSV_FIELDS = [
    "ad_id", "title", "price", "price_str", "year", "mileage_km", "gearbox",
    "fuel", "city", "area", "seller", "seller_type", "phone", "list_time",
    "nb_images", "url", "description", "options",
]


def main() -> int:
    p = argparse.ArgumentParser(description="Scraper Avito.ma (HTTP, sans navigateur)")
    p.add_argument("--category", required=True,
                   help="chemin de categorie sans /fr/ (ex: maroc/voitures-à_vendre)")
    p.add_argument("--limit", type=int, default=0, help="nb max d'annonces (0 = tout)")
    p.add_argument("--details", action="store_true",
                   help="fetch la fiche detail de chaque annonce (telephone, description)")
    p.add_argument("--delay", type=float, default=1.5, help="pause entre requetes (s)")
    p.add_argument("--jitter", type=float, default=0.8, help="alea de la pause (s)")
    p.add_argument("--out", default="", help="fichier JSONL de sortie")
    p.add_argument("--csv", default="", help="fichier CSV de sortie")
    p.add_argument("--db", action="store_true", help="ecrire dans MySQL (voir scrapper/db.py)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    client = AvitoClient(delay=args.delay, jitter=args.jitter, verbose=not args.quiet)
    t0 = time.time()

    fh = open(args.out, "w", encoding="utf-8") if args.out else None
    csvf = open(args.csv, "w", encoding="utf-8", newline="") if args.csv else None
    csvw = csv.DictWriter(csvf, fieldnames=CSV_FIELDS, extrasaction="ignore") if csvf else None
    if csvw:
        csvw.writeheader()

    db = None
    if args.db:
        from scrapper import db as dbmod
        db = dbmod.connect()
        db.ensure_schema()
        client._log(f"  DB: {db.describe()}")

    n = 0
    try:
        for ad in client.iter_listing(args.category, max_ads=args.limit):
            if args.details:
                ad = client.detail(ad)
            n += 1
            if fh:
                fh.write(json.dumps(ad, ensure_ascii=False) + "\n")
                fh.flush()
            if csvw:
                row = dict(ad)
                row["options"] = json.dumps(ad.get("options") or {}, ensure_ascii=False)
                csvw.writerow(row)
            if db:
                db.upsert_ad(ad)
                for i, img in enumerate(ad.get("images") or []):
                    db.upsert_image(ad["ad_id"], img, main=(i == 0))
            if not args.quiet and n % 20 == 0:
                client._log(f"  ... {n} annonces")
    finally:
        if fh:
            fh.close()
        if csvf:
            csvf.close()
        if db:
            db.close()

    dt = time.time() - t0
    client._log(f"\nTerminé : {n} annonces en {dt:.0f}s ({n/dt if dt else 0:.2f}/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
