"""Adaptateur SuperOccaz.ma (superoccaz.ma).

Prestashop : liste des voitures neuves/occasion `/2-accueil?page=N`.
"""

from __future__ import annotations

import html as _html
import re
from typing import Optional
from urllib.parse import urljoin

from ..core.site import SiteAdapter, make_ad

BASE = "https://superoccaz.ma"
LISTING = f"{BASE}/2-accueil"


def _kc(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


class SuperoccazAdapter(SiteAdapter):
    name = "superoccaz"
    label = "SuperOccaz.ma"
    default_category = "2-accueil"
    categories = ["2-accueil"]
    modes = ["list", "rescan", "details"]

    def __init__(self):
        self._page = 0
        super().__init__()

    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        if self._page <= 1:
            return f"{BASE}/2-accueil"
        return f"{BASE}/2-accueil?page={self._page}"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for card in re.findall(r'<article[^>]*product-miniature[^>]*>.*?</article>', html, re.S):
            url_m = re.search(r'<a href="([^"]+)" class="product-miniature__thumb-link">', card)
            if not url_m:
                continue
            url = url_m.group(1)
            ad_id_m = re.search(r'data-id-product="(\d+)"', card)
            if not ad_id_m:
                am = re.search(r"/(\d+)-[^\"/]+\.html", url)
                ad_id_m = am
            ad_id = ad_id_m.group(1) if ad_id_m else url.rstrip("/").split("/")[-1].split(".")[0]
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)
            title_m = re.search(r'<h2[^>]*class="h5 product-miniature__title[^>]*>\s*<a[^>]*>\s*(.*?)\s*</a>', card, re.S)
            h2_title = _kc(re.sub(r"<[^>]+>", " ", title_m.group(1))) if title_m else None
            strong_m = re.search(r"<p class='font-weight-bold[^'>]*text-uppercase[^>]*'>\s*(.*?)\s*</p>", card, re.S)
            strong_title = _kc(re.sub(r"<[^>]+>", " ", strong_m.group(1))) if strong_m else None
            title_parts = []
            if strong_title:
                title_parts.append(strong_title)
            if h2_title and h2_title not in (strong_title or ""):
                title_parts.append(h2_title)
            title = _kc(" ".join(title_parts)) if title_parts else None
            title = _html.unescape(title) if title else title
            price_m = re.search(r'class="price"[^>]*>\s*([\d\s\xa0]+)\s*DH', card, re.S)
            price = _num(price_m.group(1)) if price_m else None
            # attributes line
            km_m = re.search(r"km\.svg[^>]*>.*?<span[^>]*>\s*(.*?)\s*</span>", card, re.S)
            km = _num(km_m.group(1)) if km_m else None
            fuel_m = re.search(r"fuel\.svg[^>]*>.*?<span[^>]*>\s*(.*?)\s*</span>", card, re.S)
            fuel = _kc(fuel_m.group(1)) if fuel_m else None
            gearbox_m = re.search(r"gearbox\.svg[^>]*>.*?<span[^>]*>\s*(.*?)\s*</span>", card, re.S)
            gearbox = _kc(gearbox_m.group(1)) if gearbox_m else None
            year_m = re.search(r"calendar\.svg[^>]*>.*?<span[^>]*>\s*(\d{4})\s*</span>", card, re.S)
            year = int(year_m.group(1)) if year_m else None

            img_m = re.search(r'data-full-size-image-url="([^"]+)"', card)
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
                city="Tanger",
                category="Voiture d'occasion",
            )
            if thumb:
                ad["images"] = [{"url": thumb, "url_full_hd": thumb, "thumb": thumb}]
                ad["nb_images"] = 1
            out.append(ad)
        return out

    def total_ads(self, html: str) -> Optional[int]:
        m = re.search(r'<span class="total-items">\s*([\d\s]+)\s*r', html)
        if m:
            return _num(m.group(1))
        return None

    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        og = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if og:
            merged["title"] = _kc(og.group(1))

        meta_price = re.search(r'<meta property="product:price:amount" content="([^"]+)"', html)
        if meta_price:
            p = _num(meta_price.group(1))
            if p is not None:
                merged["price"] = p
                merged["price_str"] = f"{p:,}".replace(",", " ") + " DH"

        title = merged.get("title") or ad.get("title")
        if title:
            merged.setdefault("brand", title.split()[0].title() if title.split() else None)
            ym = re.search(r"\b(?:19|20)\d{2}\b", title)
            if ym:
                merged.setdefault("year", int(ym.group(0)))

        # product features
        def getf(label):
            m = re.search(r'<span class="font-weight-bold">\s*' + re.escape(label) + r'\s*:</span>\s*<span[^>]*>\s*(.*?)\s*</span>', html, re.S)
            return _kc(m.group(1)) if m else None

        if getf("Marque"):
            merged.setdefault("brand", getf("Marque"))
        if getf("Modèle"):
            merged.setdefault("model", getf("Modèle"))
        if getf("Année") and _num(getf("Année")):
            merged.setdefault("year", int(_num(getf("Année"))))
        if getf("Kilométrage"):
            merged.setdefault("mileage_km", _num(getf("Kilométrage")))
        if getf("Type de carburant"):
            merged.setdefault("fuel", getf("Type de carburant"))
        if getf("Boîte de vitesses"):
            merged.setdefault("gearbox", getf("Boîte de vitesses"))
        if merged.get("price") is None:
            mr = re.search(r'"price":\s*(\d+)', html)
            if mr:
                merged["price"] = int(mr.group(1))
        # images
        imgs = sorted({u.split("?")[0] for u in re.findall(r"https://superoccaz\.ma/\d+-large_default/[^\"]+", html)})
        if imgs:
            merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs]
            merged["nb_images"] = len(imgs)
        merged["details_fetched"] = True
        merged["ad_id"] = ad.get("ad_id")
        merged["url"] = ad.get("url") or merged.get("url")
        merged.setdefault("seller", None)
        merged.setdefault("city", "Tanger")
        return merged

    def image_url(self, img, variant="images"):
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url):
        import os, re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"

