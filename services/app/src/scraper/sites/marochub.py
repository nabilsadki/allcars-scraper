"""Adaptateur MarocHub (marochub.app)."""

from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urljoin

from ..core.site import SiteAdapter, make_ad

BASE = "https://marochub.app"


class MarochubAdapter(SiteAdapter):
    name = "marochub"
    label = "MarocHub"
    default_category = "auto"
    categories = ["auto"]
    modes = ["list", "rescan", "details"]

    def __init__(self):
        self._page = 0
        super().__init__()

    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        if self._page <= 1:
            return f"{BASE}/auto"
        return f"{BASE}/auto?page={self._page}"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        couples = []
        for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
            try:
                d = json.loads(m.group(1))
            except Exception:
                continue
            if isinstance(d, dict) and d.get("@type") == "ItemList":
                for it in (d.get("itemListElement") or []):
                    u = it.get("url")
                    name = it.get("name")
                    if u:
                        couples.append((u, name))
        for u in re.findall(r"/vehicle/[A-Za-z0-9-]+", html):
            couples.append((f"{BASE}{u}", None))

        for u, name in couples:
            full = u if u.startswith("http") else urljoin(BASE, u)
            slug = full.rstrip("/").split("/")[-1]
            ad_id = slug.rsplit("-", 1)[-1] if "-" in slug else slug
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)

            title = name
            brand = model = None
            if title:
                tparts = title.split()
                if tparts:
                    brand = tparts[0].title()
                    if len(tparts) >= 2 and not tparts[1].isdigit():
                        model = tparts[1].title()

            ad = make_ad(
                ad_id=ad_id,
                list_id=None,
                title=title,
                url=full,
                brand=brand,
                model=model,
                category="Voiture d'occasion",
            )
            out.append(ad)
        return out

    def total_ads(self, html: str) -> Optional[int]:
        return None

    def max_offset(self, total: Optional[int]) -> Optional[int]:
        return None

    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        ld = None
        for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
            try:
                d = json.loads(m.group(1))
            except Exception:
                continue
            t = d.get("@type") if isinstance(d, dict) else None
            ok = False
            if isinstance(t, list):
                ok = "Car" in t
            elif isinstance(t, str):
                ok = t in ("Car", "Product")
            if isinstance(d, dict) and ok:
                ld = d
                break
        if ld:
            merged["title"] = ld.get("name")
            merged["description"] = ld.get("description")
            brand = ld.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            if brand:
                merged["brand"] = brand
            if ld.get("model"):
                merged["model"] = ld.get("model")
            y = ld.get("vehicleModelDate")
            if y is not None:
                try:
                    merged["year"] = int(str(y)[:4])
                except (ValueError, TypeError):
                    pass
            mileage = ld.get("mileageFromOdometer")
            if isinstance(mileage, dict):
                try:
                    merged["mileage_km"] = int(float(mileage.get("value")))
                except (ValueError, TypeError):
                    pass
            elif mileage is not None:
                try:
                    merged["mileage_km"] = int(float(mileage))
                except (ValueError, TypeError):
                    pass
            if ld.get("fuelType"):
                ft = ld.get("fuelType")
                merged["fuel"] = str(ft).title() if isinstance(ft, str) else ft
            if ld.get("vehicleTransmission"):
                v = ld.get("vehicleTransmission")
                merged["gearbox"] = str(v).title() if isinstance(v, str) else v
            if ld.get("color"):
                merged["color"] = ld.get("color")
            offers = ld.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price")
                if price is not None:
                    try:
                        merged["price"] = int(float(price))
                    except (ValueError, TypeError):
                        pass
                if merged.get("price") is not None:
                    merged["price_str"] = f"{merged['price']:,}".replace(",", " ") + " MAD"
            imgs = ld.get("image")
            if isinstance(imgs, list):
                imgs = [str(u) for u in imgs if u]
                merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs]
                merged["nb_images"] = len(imgs)
            elif isinstance(imgs, str):
                merged["images"] = [{"url": imgs, "url_full_hd": imgs, "thumb": imgs}]
                merged["nb_images"] = 1

        city_m = re.search(r'map-pin.*?</svg>\s*<span[^>]*>([^<]+)</span>', html, re.S)
        if city_m:
            merged.setdefault("city", city_m.group(1).strip().title())

        # pas de capture vendeur fiable dans le HTML visible ; la fiche
        # JSON-LD ne l'expose pas non plus. On laisse la valeur par defaut.

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
