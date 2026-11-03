"""Adaptateur M·OCCAZ (m-occaz.ma).

Petit parc de voitures (Volkswagen, Dacia, etc.) a Tanger. La page
`/notre-parc-de-vehicules` renvoie l'ensemble des vehicules en cours
d'affichage (environ 20) ; aucune pagination fonctionnelle.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.m-occaz.ma"
LISTING = f"{BASE}/notre-parc-de-vehicules"


def _kc(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _price(text: str) -> Optional[int]:
    if not text:
        return None
    # "221 000,00 Dhs" -> 221000 (on ignore les centimes)
    if "," in text:
        text = text.split(",", 1)[0]
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


class MoccazAdapter(SiteAdapter):
    name = "m_occaz"
    label = "M.OCCAZ"
    default_category = "nos-voitures"
    categories = ["nos-voitures"]
    modes = ["list"]

    def __init__(self):
        self._page = 0
        super().__init__()

    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        return f"{BASE}/notre-parc-de-vehicules"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for block in re.findall(r"<!-- Start - Car Card -->(.*?)<!-- End - Car Card -->", html, re.S):
            title_m = re.search(r'<h2 class="car-title[^>]*>\s*(.*?)\s*</h2>', block, re.S)
            title = _kc(re.sub(r"<[^>]+>", " ", title_m.group(1))) if title_m else None
            subtitle_m = re.search(r'<p class="car-subtitle">\s*(.*?)\s*</p>', block, re.S)
            subtitle = _kc(re.sub(r"<[^>]+>", " ", subtitle_m.group(1))) if subtitle_m else None

            url_m = re.search(r'href="([^"]+/annonce/[^"]+)"', block)
            url = url_m.group(1) if url_m else None
            if url and not url.startswith("http"):
                url = urljoin(BASE, url)
            ad_id_m = re.search(r"/annonce/[^/\-]+[^/]*-(\d+)(?:\?|$)", url or "")
            if not ad_id_m:
                ad_id_m = re.search(r"/annonce/[^\"]*", block)
                ad_id = re.search(r"-(\d+)(?:\?|$)", url or "").group(1) if (url and re.search(r"-(\d+)(?:\?|$)", url)) else None
            else:
                ad_id = ad_id_m.group(1)
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)

            price_m = re.search(r'<div class="price">\s*([\d\s.,]+)', block)
            price = _price(price_m.group(1)) if price_m else None

            year_m = re.search(r'year\.svg[^>]*>.*?<span[^>]*>\s*(\d{4})', block, re.S | re.I)
            year = int(year_m.group(1)) if year_m else None

            km_m = re.search(r'kilometrag\.svg[^>]*>.*?<span[^>]*>\s*(.*?)\s*</span>', block, re.S | re.I)
            km = _num(km_m.group(1)) if km_m else None

            fuel_m = re.search(r'carburant\.svg[^>]*>.*?<span[^>]*>\s*(.*?)\s*</span>', block, re.S | re.I)
            fuel = _kc(fuel_m.group(1)) if fuel_m else None

            gearbox_m = re.search(r'boite\.svg[^>]*>.*?<span[^>]*>\s*(.*?)\s*</span>', block, re.S | re.I)
            gearbox = _kc(gearbox_m.group(1)) if gearbox_m else None

            img_m = re.search(r'data-src="(https://cloud\.car-cutter\.com/vehicles_images/[^"]+\.jpg)"', block)
            thumb = img_m.group(1) if img_m else None

            brand = model = None
            if title:
                tparts = title.split()
                if tparts:
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
                year=year,
                mileage_km=km,
                fuel=fuel,
                gearbox=gearbox,
                brand=brand,
                model=model,
                city=None,
                seller="M.OCCAZ",
                seller_type="Professionnel",
                category="Voiture d'occasion",
            )
            if thumb:
                ad["images"] = [{"url": thumb, "url_full_hd": thumb, "thumb": thumb}]
                ad["nb_images"] = 1
            out.append(ad)
        return out

    def total_ads(self, html: str) -> Optional[int]:
        # la premiere page contient l'integralite du stock (20 annonces)
        n = len(re.findall(r"<!-- Start - Car Card -->", html))
        return n if n > 0 else None

    def detail_url(self, ad: dict) -> Optional[str]:
        return None

    def parse_detail(self, html: str, ad: dict) -> dict:
        return {"details_fetched": True}

    def image_url(self, img, variant="images"):
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url):
        import os, re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"
