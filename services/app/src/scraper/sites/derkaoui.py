"""Adaptateur Derkaoui Automotive (derkaouiautomotive.ma).

Site WooCommerce de l'agence Derkaoui Automotive (Tanger). Catalogue
`/nos-voitures/?product-page=N`, pages de detail `/produit/<slug>/`.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin
import html as _html

from ..core.site import SiteAdapter, make_ad

BASE = "https://derkaouiautomotive.ma"
LISTING = f"{BASE}/nos-voitures/"


def _kc(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _num(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _price(text: str) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


class DerkaouiAdapter(SiteAdapter):
    name = "derkaoui"
    label = "Derkaoui Automotive"
    default_category = "nos-voitures"
    categories = ["nos-voitures"]
    modes = ["list", "rescan", "details"]

    def __init__(self):
        self._page = 0
        super().__init__()

    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        url = category if category.startswith("http") else f"{BASE}/{category}/" if not category.startswith("/") else f"{BASE}{category}"
        # normalise the configured category ("nos-voitures") :
        base = f"{BASE}/nos-voitures/"
        if self._page <= 1:
            return base
        return f"{base}?product-page={self._page}"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for li in re.findall(r'<li[^>]*class="product[^"]*"[^>]*>.*?</li>', html, re.S):
            url_m = re.search(r'href="(https://derkaouiautomotive\.ma/produit/[^"]+)"', li)
            if not url_m:
                continue
            url = url_m.group(1)
            ad_id_m = re.search(r'post-(\d+)', li)
            ad_id = ad_id_m.group(1) if ad_id_m else url.rstrip("/").split("/")[-1]
            if ad_id in seen:
                continue
            seen.add(ad_id)

            title_m = re.search(r'<h2[^>]*class="woocommerce-loop-product__title"[^>]*>\s*(.*?)\s*</h2>', li, re.S)
            title = _kc(re.sub(r"<[^>]+>", "", title_m.group(1))) if title_m else None
            title = _html.unescape(title) if title else title
            year = None
            if title:
                ym = re.search(r"\b(?:19|20)\d{2}\b", title)
                if ym:
                    year = int(ym.group(0))

            price_m = re.search(r'<span class="woocommerce-Price-amount amount">\s*<bdi>([\d\s.]+)', li, re.S)
            price = _price(price_m.group(1)) if price_m else None
            km = None

            img_m = re.search(r'<img[^>]+src="(https://derkaouiautomotive\.ma/wp-content/uploads/[^"]+)"', li)
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
        # le nombre total de pages n'est pas expose facilement ; pagination
        # s'arrete sur page vide
        return None

    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if h1:
            title = _kc(re.sub(r"<[^>]+>", " ", h1.group(1)))
            merged["title"] = _html.unescape(title)
            mm = re.search(r"(\d{4})\s*[–-]\s*(\d{4})", title)
            if mm:
                merged.setdefault("year", int(mm.group(1)))

        desc_m = re.search(r'<p class="p1">\s*<span class="s1">(.*?)</span>\s*</p>', html, re.S)
        if desc_m:
            block = desc_m.group(1)
            merged["description"] = _kc(re.sub(r"<br\s*/?>", " | ", block))
            my = re.search(r"Ann[ée]e\s*[–-]\s*(\d{4})\b", block, re.I)
            if my:
                merged["year"] = int(my.group(1))
            km_m = re.search(r"Kilom[ée]trage\s*[–-]\s*([\d\s.]+)\s*KM", block, re.I)
            if km_m:
                merged["mileage_km"] = _num(km_m.group(1))
            motor_m = re.search(r"Motorisation\s*[–-]\s*(.+?)(?:<br|Kilom[ée]trage|Version)", block, re.I)
            if motor_m:
                merged["fuel"] = None
                # keep raw in description; fuel light
            motor2 = re.search(r"Motorisation\s*[–-]\s*(.+?)(?:<br)", block, re.I)
            if motor2:
                txt = _kc(motor2.group(1))
                low = txt.lower()
                if "diesel" in low:
                    merged["fuel"] = "Diesel"
                elif "essence" in low:
                    merged["fuel"] = "Essence"
                elif "hybride" in low:
                    merged["fuel"] = "Hybride"
            version_m = re.search(r"Version\s*[–-]\s*(.+?)(?:<br|Motorisation)", block, re.I)
            if version_m:
                # attach as option label
                merged.setdefault("option_labels", []).append(_kc(version_m.group(1)))

        price_m = re.search(r'<p class="price">\s*<span class="woocommerce-Price-amount amount">\s*<bdi>([\d\s.]+)', html, re.S)
        if price_m:
            price = _price(price_m.group(1))
            if price:
                merged["price"] = price
                merged["price_str"] = f"{price:,}".replace(",", " ") + " DH"

        if "mileage_km" not in merged:
            km_m = re.search(r'Kilom[ée]trage\s*[–-]\s*([\d\s.]+)\s*KM', html, re.I)
            if not km_m:
                km_m = re.search(r'(\d[\d\s.]*?)\s*KM\b', html, re.I)
            if km_m:
                k = _num(km_m.group(1))
                if k is not None:
                    merged["mileage_km"] = k
        if "fuel" not in merged:
            low = html.lower()
            if "diesel" in low:
                merged["fuel"] = "Diesel"
            elif "hybride" in low:
                merged["fuel"] = "Hybride"
            elif "essence" in low:
                merged["fuel"] = "Essence"
        if "year" not in merged:
            ym = re.search(r'Ann[ée]e\s*[–-]\s*(\d{4})', html, re.I)
            if ym:
                merged["year"] = int(ym.group(1))

        title = merged.get("title") or ad.get("title")
        if title:
            tparts = title.split()
            if tparts:
                merged.setdefault("brand", tparts[0].title())
                if len(tparts) > 1:
                    merged.setdefault("model", " ".join(tparts[1:]).title())

        # city : agence a Tanger, tout produit a Tanger
        merged.setdefault("city", "Tanger")
        merged.setdefault("seller_type", None)
        merged.setdefault("seller", "Derkaoui Automotive")

        # images (full-size, sans les vignettes)
        raw_imgs = re.findall(r"https://derkaouiautomotive\.ma/wp-content/uploads/[^\"]+\.(?:jpe?g|png|webp)", html, re.I)
        full = sorted({u.split("?")[0] for u in raw_imgs if not re.search(r"-\d+x\d+\.", u)})
        if full:
            merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in full]
            merged["nb_images"] = len(full)

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

