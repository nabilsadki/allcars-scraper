"""Adaptateur Autobroker (autobroker.ma).

Le site affiche toutes ses annonces sur une seule page
(/autobroker-liste-vehicules) : chaque card est un <li class="item"> puis
la fiche complete est une page /autobroker-fiche-vehicule/<id>-<slug>.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.autobroker.ma"
LISTING = f"{BASE}/autobroker-liste-vehicules"


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _price(text: str) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


class AutobrokerAdapter(SiteAdapter):
    name = "autobroker"
    label = "Autobroker.ma"
    default_category = "vehicule-occasion"
    categories = ["vehicule-occasion"]
    modes = ["list", "rescan", "details"]

    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        return LISTING

    def total_ads(self, html: str) -> Optional[int]:
        try:
            return len(re.findall(r'<li class="item[^"]*">', html))
        except Exception:
            return None

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for frag in re.findall(r'<li class="item[^"]*">(.*?)</li>', html, re.S):
            url_m = re.search(r'href="(/autobroker-fiche-vehicule/(\d+)-[^"]*)"', frag)
            if not url_m:
                continue
            url = BASE + url_m.group(1)
            ad_id = url_m.group(2)
            if ad_id in seen:
                continue
            seen.add(ad_id)

            title_m = re.search(r'<div class="item-title">\s*<a[^>]*>(.*?)</a>', frag, re.S)
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title_m.group(1))).strip() if title_m else None

            price_m = re.search(r'class="price">\s*([^<]+)Dhs', frag, re.S)
            price = _price(price_m.group(1)) if price_m else None

            km = None
            km_m = re.search(r'col-km[^>]*>.*?(\d[\d\s]*)\s*km', frag, re.S | re.I)
            if km_m:
                km = _num(km_m.group(1))

            gear = None
            gear_m = re.search(r'col-engine[^>]*>.*?<i[^>]*></i>\s*(.*?)\s*</div>', frag, re.S)
            if gear_m:
                gear = gear_m.group(1).strip()

            year = None
            year_m = re.search(r'col-date[^>]*>.*?<i[^>]*></i>\s*(.*?)\s*</div>', frag, re.S)
            if year_m:
                txt = year_m.group(1).strip()
                y = re.search(r'(?:19|20)\d{2}', txt)
                if y:
                    year = int(y.group(0))

            fuel = None
            sold = "Vendue" in frag or "Vendu" in frag
            img_m = re.search(r'img src="(https://admin\.autobroker\.ma/img/cars/[^"]+)"', frag)
            thumb = img_m.group(1) if img_m else None

            brand = model = None
            if title:
                parts = title.split()
                if parts:
                    brand = parts[0].title()
                    model_tokens = parts[1:]
                    if model_tokens:
                        model = " ".join(model_tokens).title()

            ad = make_ad(
                ad_id=ad_id,
                list_id=None,
                title=title,
                url=url,
                price=price,
                price_str=(f"{price:,}".replace(",", " ") + " Dhs") if price else None,
                year=year,
                mileage_km=km,
                fuel=fuel,
                gearbox=gear.title() if gear else None,
                brand=brand,
                model=model,
                category="Voiture d'occasion",
                seller="Autobroker",
                seller_type="Pro",
                is_active=(not sold),
            )
            if thumb:
                ad["images"] = [{"url": thumb, "url_full_hd": thumb, "thumb": thumb}]
                ad["nb_images"] = 1
            out.append(ad)
        return out

    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        title_m = re.search(r'<div class="item-title">\s*<a[^>]*>(.*?)</a>', html, re.S)
        if title_m:
            title = re.sub(r"<[^>]+>", " ", title_m.group(1))
            title = re.sub(r"\s+", " ", title).strip()
            merged["title"] = title
            tparts = title.split()
            if tparts:
                merged.setdefault("brand", tparts[0].title())
                model_tokens = tparts[1:]
                if model_tokens:
                    merged.setdefault("model", " ".join(model_tokens).title())

        price_m = re.search(r'class="price">\s*([\d\s]+)Dhs', html, re.S)
        if price_m:
            price = _price(price_m.group(1))
            if price:
                merged["price"] = price
                merged["price_str"] = f"{price:,}".replace(",", " ") + " Dhs"

        # label/value specs
        spec = {}
        for m in re.finditer(r'<td class="label-spec"[^>]*>\s*(.*?)\s*<span class="coln">:</span>\s*</td>\s*<td class="value-spec">\s*(.*?)\s*</td>',
                             html, re.S):
            k = re.sub(r"<[^>]+>", "", m.group(1)).strip().lower()
            v = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
            if k:
                spec[k] = v

        def g(key):
            for kk, vv in spec.items():
                if kk == key.lower():
                    return vv
            return None

        if g("carburant"):
            merged["fuel"] = g("carburant").title()
        bt = g("boîte à vitesse") or g("boite a vitesse")
        if bt:
            merged["gearbox"] = bt.title()
        if g("kilométrage"):
            merged["mileage_km"] = _num(g("kilométrage"))
        if g("cheval fiscal"):
            fp = _num(g("cheval fiscal"))
            if fp is not None:
                merged["fiscal_power"] = fp
        if g("couleur"):
            merged["colour"] = g("couleur").title()
        # year not in detail specs; keep ad's possibly listing year if any
        # images
        ad_id = ad.get("ad_id")
        if ad_id:
            imgs = sorted(set(re.findall(
                rf"admin\.autobroker\.ma/img/cars/{ad_id}-\d+\.jpe?g", html)))
            # convert to full URL
            imgs = [u if u.startswith("http") else f"https://{u}" for u in imgs]
            if imgs:
                merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs]
                merged["nb_images"] = len(imgs)
        # sold status
        if "Vendue" in html or "Vendu" in html:
            merged["is_active"] = False
        ad_id2 = ad.get("ad_id")
        if ad_id2:
            merged["ad_id"] = ad_id2
        merged["seller"] = "Autobroker"
        merged["seller_type"] = "Pro"
        merged.setdefault("url", ad.get("url"))
        merged["details_fetched"] = True
        return merged

    def image_url(self, img, variant="images"):
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url):
        import os, re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"
