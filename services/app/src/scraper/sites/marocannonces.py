"""Adaptateur MarocAnnonces.com.

Petite annonces tous categories. La categorie voitures est a :
  https://www.marocannonces.com/maroc/voitures-occasion-b314.html
Pagination : ?pge=N.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from ..core.site import SiteAdapter, make_ad

BASE = "https://www.marocannonces.com"
DEFAULT_PATH = "maroc/voitures-occasion-b314.html"


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


class MarocannoncesAdapter(SiteAdapter):
    name = "marocannonces"
    label = "MarocAnnonces.com"
    default_category = DEFAULT_PATH
    categories = [DEFAULT_PATH]
    modes = ["list", "rescan", "details"]

    def __init__(self):
        self._page = 0
        super().__init__()

    def reset_pagination(self) -> None:
        self._page = 0

    def listing_url(self, category: str, offset: int) -> str:
        self._page = getattr(self, "_page", 0) + 1
        url = f"{BASE}/{category}"
        if self._page <= 1:
            return url
        return f"{url}?pge={self._page}"

    def parse_listing(self, html: str, category: str = "") -> list[dict]:
        out = []
        seen = set()
        for li in re.findall(r"<li[^>]*>.*?</li>", html, re.S):
            if "annonce/" not in li:
                continue
            url_m = re.search(r'href="([^"]+/annonce/(\d+)/[^"]+\.html)"', li)
            if not url_m:
                continue
            url = urljoin(BASE, url_m.group(1))
            ad_id = url_m.group(2)
            # ne garder que les annonces de la categorie Voitures (314),
            # meme si la page visitee est une autre (garde-fou contre la
            # pollution croisee : emploi, immobilier, services, ...).
            # attention : les href sont relatifs ("categorie/314/..."), on
            # teste donc l'URL absolue et non le href brut.
            if "/categorie/314/" not in url:
                continue
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)

            title_m = re.search(r"<h3[^>]*>\s*(.*?)\s*</h3>", li, re.S)
            title_attr = re.search(r'<a[^>]+title="([^"]+)"', li)
            title = _kc(title_m.group(1) if title_m else (title_attr.group(1) if title_attr else None))
            title = title or None

            price_m = re.search(r'<strong class="price">\s*(.*?)\s*</strong>', li, re.S)
            price = _price(price_m.group(1)) if price_m else None

            loc_m = re.search(r'<span class="location">\s*(.*?)\s*</span>', li, re.S)
            city = _kc(loc_m.group(1)) if loc_m else None

            img_m = re.search(r'<img[^>]+data-original="([^"]+)"', li) or re.search(r'<img[^>]+src="([^"]+)"', li)
            thumb = img_m.group(1) if img_m else None
            if thumb and thumb.startswith("/"):
                thumb = urljoin(BASE, thumb)

            brand = model = None
            if title:
                tparts = title.split()
                if tparts:
                    brand = tparts[0].title()
                    if len(tparts) > 1:
                        model = tparts[1].title()

            ad = make_ad(
                ad_id=ad_id,
                list_id=None,
                title=title,
                url=url,
                price=price,
                price_str=(f"{price:,}".replace(",", " ") + " DH") if price else None,
                city=city,
                brand=brand,
                model=model,
                category="Voiture d'occasion",
            )
            if thumb:
                ad["images"] = [{"url": thumb, "url_full_hd": thumb, "thumb": thumb}]
                ad["nb_images"] = 1
            out.append(ad)
        return out

    def total_ads(self, html: str) -> Optional[int]:
        # pas de compteur global clair : on s'arrete sur page vide
        return None

    def detail_url(self, ad: dict) -> Optional[str]:
        return ad.get("url")

    def parse_detail(self, html: str, ad: dict) -> dict:
        merged = {}
        title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if title_m:
            merged["title"] = _kc(title_m.group(1))

        # announce metadata list
        for m in re.finditer(r"<li>\s*([^:<]+?)\s*:\s*<a[^>]*>(.*?)</a>\s*</li>", html, re.S):
            k = _kc(m.group(1)).lower()
            v = _kc(re.sub(r"<[^>]+>", "", m.group(2)))
            if "année" in k or "annee" in k:
                y = _num(v)
                if y is not None:
                    merged["year"] = y
            elif "kilométrage" in k or "kilometrage" in k:
                km = _num(v)
                if km is not None:
                    merged["mileage_km"] = km
            elif "carburant" in k:
                merged["fuel"] = v.title()
            elif "ville" in k:
                merged.setdefault("city", v.title())

        list_price = re.search(r'"price">([\d\s.]+) DH</strong>', html)
        if not list_price:
            list_price = re.search(r"<strong[^>]*>([\d\s.]+)\s*DH</strong>", html)
        if list_price:
            price = _price(list_price.group(1))
            if price:
                merged["price"] = price
                merged["price_str"] = f"{price:,}".replace(",", " ") + " DH"

        desc_m = re.search(r"Detail de l'annonce.*?</strong>\s*(.*?)(?:<br|</dd>|</div>)", html, re.S)
        if desc_m:
            merged["description"] = _kc(re.sub(r"<[^>]+>", " ", desc_m.group(1)))

        # seller type ("Particulier"/"Professionnel") dans la description
        desc_text = merged.get("description") or ""
        for t in ("Particulier", "Professionnel"):
            if t.lower() in desc_text.lower() or t.lower() in html.lower():
                # trop ambigue : ne remplir que si on trouve un mot isole
                break
        # tel (masque : on laisse le bouton sauf si un numero complet est trouve)
        phone_m = re.search(r"\b(0[567]\d{8})\b", html)
        if phone_m:
            merged["phone"] = phone_m.group(1)

        # images
        imgs = sorted(set(re.findall(r"https?://images\.marocannonces\.com/[^\"]+", html)))
        imgs = [u.split("?")[0] for u in imgs]
        if imgs:
            merged["images"] = [{"url": u, "url_full_hd": u, "thumb": u} for u in imgs]
            merged["nb_images"] = len(imgs)

        merged["details_fetched"] = True
        merged["ad_id"] = ad.get("ad_id")
        merged["url"] = ad.get("url") or merged.get("url")
        merged.setdefault("seller_type", None)
        merged.setdefault("fuel", ad.get("fuel"))
        return merged

    def image_url(self, img, variant="images"):
        return img.get("url") or img.get("url_full_hd") or ""

    def image_id(self, url):
        import os, re as _re
        name = os.path.basename(url.split("?")[0])
        return _re.sub(r"[^0-9A-Za-z_.-]", "_", name) or "img"

