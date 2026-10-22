#!/usr/bin/env python3
"""
Test de parité STRICT : meme URL, deux chemins d'extraction.

  A) HTTP  : curl_cffi + __NEXT_DATA__      (nouveau)
  B) Selenium : Firefox headless + JSON-LD  (ancien, tel qu'il etait code)

On compare champ par champ sur 3 annonces reelles du catalogue actuel.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapper.sites.avito_http import AvitoClient  # noqa: E402

from selenium import webdriver  # noqa: E402
from selenium.webdriver.firefox.options import Options  # noqa: E402
from selenium.webdriver.firefox.service import Service  # noqa: E402


def selenium_ads(urls):
    """Ancien chemin : rendu navigateur + extraction JSON-LD du DOM."""
    opts = Options()
    opts.add_argument("--headless")
    svc = Service("/snap/bin/geckodriver")
    drv = webdriver.Firefox(options=opts, service=svc)
    out = []
    try:
        for u in urls:
            drv.get(u)
            time.sleep(2.0)
            html = drv.page_source
            for m in re.finditer(
                r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
            ):
                try:
                    d = json.loads(m.group(1))
                except Exception:
                    continue
                if d.get("@type") != "Product":
                    continue
                off = d.get("offers") or {}
                out.append({
                    "url": u,
                    "title": (d.get("name") or "").strip(),
                    "price": off.get("price"),
                    "currency": off.get("priceCurrency"),
                    "images": d.get("image") or [],
                    "seller": (off.get("seller") or {}).get("name"),
                    "description": (d.get("description") or "").strip(),
                })
                break
    finally:
        drv.quit()
    return out


def main():
    c = AvitoClient(delay=1.0, jitter=0.5, verbose=True)
    page = c.listing_page("maroc/voitures-à_vendre", offset=0)
    picks = [a for a in page["ads"] if a.get("price")][:3]
    print(f"Annonces retenues : {[a['ad_id'] for a in picks]}")

    print("\n--- A) extraction HTTP (nouveau) ---")
    http_rows = []
    for a in picks:
        d = c.detail(a)
        http_rows.append(d)
        print(f"  {d['ad_id']} | {d['title'][:38]:38s} | {d['price_str']:>12s} | "
              f"img={d['nb_images']} | tel={d.get('phone')}")

    print("\n--- B) extraction Selenium (ancien) ---")
    urls = [r["url"] for r in http_rows]
    sel_rows = selenium_ads(urls)
    for r in sel_rows:
        print(f"  {r['title'][:38]:38s} | {str(r['price']):>12s} {r['currency']} | "
              f"img={len(r['images'])} | vendeur={r['seller']}")

    print("\n" + "=" * 72)
    print("COMPARAISON")
    ok = 0
    for h in http_rows:
        s = next((x for x in sel_rows if norm(h["title"]) == norm(x["title"])), None)
        if not s:
            print(f"  {h['ad_id']} : non retrouvé côté Selenium")
            continue
        cmp = {
            "titre": norm(h["title"]) == norm(s["title"]),
            "prix": float(h["price"] or 0) == float(s["price"] or 0),
            "vendeur": norm(h.get("seller")) == norm(s.get("seller")),
            "1re image": (os.path.basename((h["images"][0]["url"] if h["images"] else ""))
                          == os.path.basename((s["images"][0] if s["images"] else ""))),
            "description": norm(h.get("description"))[:200] == norm(s.get("description"))[:200],
        }
        good = all(cmp.values())
        ok += good
        print(f"  {h['ad_id']} : {'IDENTIQUE' if good else 'ECARTS'}  " +
              " ".join(f"{k}={'ok' if v else 'KO'}" for k, v in cmp.items()))
        if not good:
            print(f"      HTTP   : titre={h['title'][:40]!r} prix={h['price']} "
                  f"img1={(h['images'][0]['url'] if h['images'] else None)}")
            print(f"      Selenium: titre={s['title'][:40]!r} prix={s['price']} "
                  f"img1={(s['images'][0] if s['images'] else None)}")
    print("=" * 72)
    print(f"PARITÉ : {ok}/{len(http_rows)} annonces strictement identiques")
    return 0 if ok == len(http_rows) else 1


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().strip())


if __name__ == "__main__":
    sys.exit(main())
