#!/usr/bin/env python3
"""
Test de parité : compare les données de l'ancien scraper Selenium (test.htm,
rendu par le navigateur en juin 2023) avec les données extraites en HTTP.

On compare les annonces présentes dans les deux sources sur les champs
communs (titre, prix, ville, ...) pour valider que le HTTP renvoie bien
la meme information, sans navigateur.
"""
import json
import re
import sys
import os
import html as htmllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapper.sites.avito_http import AvitoClient  # noqa: E402

OLD = "/var/www/html/selenium/test.htm"


def old_ads(path=OLD):
    """Extrait les annonces de l'ancien HTML rendu par Selenium (JSON-LD)."""
    h = open(path, encoding="utf-8", errors="replace").read()
    out = []
    for m in re.finditer(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>', h, re.S
    ):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if d.get("@type") != "Product":
            continue
        off = d.get("offers") or {}
        seller = off.get("seller") or {}
        out.append({
            "title": (d.get("name") or "").strip(),
            "price": off.get("price"),
            "currency": off.get("priceCurrency"),
            "images": d.get("image") or [],
            "url": d.get("url"),
            "seller": seller.get("name"),
            "description": (d.get("description") or "").strip(),
        })
    return out


def norm(s):
    s = (s or "").lower().strip()
    return re.sub(r"\s+", " ", s)


def main():
    print("=" * 70)
    print("1. Extraction de l'ancien HTML (Selenium, juin 2023)")
    olds = old_ads()
    print(f"   annonces trouvées : {len(olds)}")
    if not olds:
        print("   rien à comparer")
        return 1

    print("=" * 70)
    print("2. Extraction HTTP (curl_cffi + __NEXT_DATA__)")
    c = AvitoClient(delay=1.0, jitter=0.5)
    # on prend une catégorie large pour maximiser le recouvrement
    ads = []
    for off in (0, 38, 76, 114, 152, 190):
        page = c.listing_page("maroc/voitures-à_vendre", offset=off)
        ads.extend(page["ads"])
    print(f"   annonces récupérées : {len(ads)}")

    # index HTTP par titre normalisé
    index = {}
    for a in ads:
        index.setdefault(norm(a["title"]), []).append(a)

    print("=" * 70)
    print("3. Recouvrement titre × titre")
    matched, price_ok, price_bad, seller_ok, url_ok, img_ok = 0, 0, 0, 0, 0, 0
    details = []
    for o in olds:
        cand = index.get(norm(o["title"]))
        if not cand:
            continue
        matched += 1
        h = cand[0]
        po, ph = o.get("price"), h.get("price")
        if po and ph and abs(float(po) - float(ph)) < 1:
            price_ok += 1
        else:
            price_bad += 1
            details.append((o["title"][:45], po, ph))
        if norm(o.get("seller")) and norm(o.get("seller")) == norm(h.get("seller")):
            seller_ok += 1
        if o.get("url") and h.get("url") and o["url"].rstrip("/") == h["url"].rstrip("/"):
            url_ok += 1
        if o.get("images") and h.get("images"):
            if os.path.basename(o["images"][0]) == os.path.basename(h["images"][0]["url"]):
                img_ok += 1

    print(f"   titres Appariés        : {matched}/{len(olds)}")
    print(f"   prix identiques        : {price_ok}/{matched}")
    print(f"   prix divergents        : {price_bad}")
    print(f"   vendeur identique      : {seller_ok}/{matched}")
    print(f"   URL identique          : {url_ok}/{matched}")
    print(f"   1re image identique    : {img_ok}/{matched}")
    if details:
        print("\n   écarts de prix (ancien -> nouveau) :")
        for t, po, ph in details[:8]:
            print(f"     {t:47s} {po} -> {ph}")

    print("=" * 70)
    verdict = "PARITÉ OK" if matched and price_bad == 0 else (
        "PARITÉ PARTIELLE" if matched else "AUCUN RECOUVREMENT")
    print(f"VERDICT : {verdict}")
    return 0 if matched else 1


if __name__ == "__main__":
    sys.exit(main())
