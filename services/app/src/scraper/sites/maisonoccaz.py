"""Adaptateur Maison Occaz (maisonoccaz.ma).

Catalogue `/acheter?search=&page=N`, fiches `/voiture/<slug>`.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.maisonoccaz.ma"
LISTING = f"{BASE}/acheter"


def _kc(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


class MaisonoccazAdapter(SiteAdapter):
    name = "maisonoccaz"
    label = "Maison Occaz"
    default_category = "acheter"
    categories = ["acheter"]
    modes = ["list", "rescan", "details"]

    def __init__(self):
        self._page = 0
        super().__init__()

    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        if self._page <= 1:
            return f"{BASE}/acheter"
        return f"{BASE}/acheter?search=&page={self._page}"

    def _spec_block(self, block: str, icon: str) -> Optional[str]:
        m = re.search(r'<i class="fa-solid fa-' + re.escape(icon) + r'"></i>\s*(.*?)\s*(?:</span>|</div>)', block, re.S)
        if not m:
            return None
        return _kc(m.group(1))

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        starts = [m.start() for m in re.finditer(r'<div class="car-card">', html)]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(html)
            block = html[start:end]
            img_m = re.search(r'<img src="([^"]+)"', block)
            thumb = img_m.group(1) if img_m else None
            if thumb and not thumb.startswith("http"):
                thumb = urljoin(BASE, thumb)

            url_m = re.search(r'<a class="discover" href="([^"]+)"', block)
            url = urljoin(BASE, url_m.group(1)) if url_m else None
            ad_id = (url.rstrip("/").split("/")[-1] if url else None)
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)

            titles = re.findall(r'<h3>\s*(.*?)\s*</h3>', block, re.S)
            title = _kc(titles[0]) if titles else None
            price = None
            price_ms = re.findall(r'<h3>\s*([\d\s,\.]+)\s*DHs?\s*</h3>', block, re.S)
            if price_ms:
                price = _num(price_ms[-1])
                if price is not None and price < 10000 and len(price_ms) > 1:
                    price = _num(price_ms[-2])

            km_m = re.search(r'<i class="fa-solid fa-road"></i>\s*([\d\s]+)\s*km', block, re.I)
            km = _num(km_m.group(1)) if km_m else None
            gear_m = re.search(r'<i class="fa-solid fa-gear"></i>\s*(.*?)\s*</span>', block, re.S)
            gearbox = _kc(gear_m.group(1)) if gear_m else None
            fuel_m = re.search(r'<i class="fa-solid fa-gas-pump"></i>\s*(.*?)\s*</span>', block, re.S)
            fuel = _kc(fuel_m.group(1)) if fuel_m else None

            brand = model = None
            if title:
                tparts = title.split()
                if tparts and len(tparts[0]) > 1:
                    brand = tparts[0].title()
                    if len(tparts) > 1:
                        model = " ".join(tparts[1:]).title()

            ad = make_ad(
                ad_id=ad_id,
                list_id=None,
                title=title,
                url=url,
                price=price,
                price_str=(f"{price:,}".replace(",", " ") + " DH") if price else None,
                year=None,
                mileage_km=km,
                fuel=fuel,
                gearbox=gearbox,
                brand=brand,
                model=model,
                city=None,
                seller="Maison Occaz",
                seller_type="Professionnel",
                category="Voiture d'occasion",
            )
            if thumb:
                ad["images"] = [{"url": thumb, "url_full_hd": thumb, "thumb": thumb}]
                ad["nb_images"] = 1
            out.append(ad)
        return out

    def total_ads(self, html: str) -> Optional[int]:
        return None

    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        title_m = re.search(r'<h1[^>]*>\s*(.*?)\s*</h1>', html, re.S)
        if title_m:
            title = _kc(re.sub(r"<[^>]+>", " ", title_m.group(1)))
            merged["title"] = title

        title = merged.get("title") or ad.get("title")
        if title:
            ym = re.search(r"\b(?:19|20)\d{2}\b", title)
            if ym:
                merged.setdefault("year", int(ym.group(0)))

        price_ms = re.findall(r'<h3>\s*([\d\s,\.]+)\s*DHs?\s*</h3>', html, re.S)
        if price_ms:
            price = _num(price_ms[-1])
            if price is not None and price >= 10000:
                merged["price"] = price
                merged["price_str"] = f"{price:,}".replace(",", " ") + " DH"

        def spec(icon, pattern):
            m = re.search(pattern, html, re.S)
            return _kc(m.group(1)) if m else None

        km = spec("road", r'<i class="fa fa-road"></i>\s*([\d\s]+)\s*km')
        if km:
            merged["mileage_km"] = _num(km)
        fuel = spec("gas-pump", r'<i class="fa fa-gas-pump"></i>\s*(.*?)\s*</div>')
        if fuel:
            merged["fuel"] = fuel
        gearbox = spec("gears", r'<i class="fa fa-gears"></i>\s*(.*?)\s*</div>')
        if gearbox:
            merged["gearbox"] = gearbox
        year_m = re.search(r'<i class="fa fa-calendar"></i>\s*(\d{4})', html)
        if year_m:
            merged.setdefault("year", int(year_m.group(1)))

        desc_m = re.search(r'<p class="description">(.*?)</p>', html, re.S)
        if desc_m:
            merged["description"] = _kc(re.sub(r"<[^>]+>", " ", desc_m.group(1)))

        imgs = sorted({u.split("?")[0] for u in re.findall(r'/uploads/cars/[^"\']+\.(?:jpe?g|png|webp)', html, re.I)})
        imgs = [urljoin(BASE, u) for u in imgs]
        if imgs:
            merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs]
            merged["nb_images"] = len(imgs)

        merged.setdefault("seller", "Maison Occaz")
        merged.setdefault("seller_type", "Professionnel")
        merged.setdefault("city", None)
        merged["details_fetched"] = True
        merged["ad_id"] = ad.get("ad_id")
        merged["url"] = ad.get("url") or merged.get("url")
        return merged

    def image_url(self, img, variant="images"):
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url):
        import os, re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"
